import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..account_protocol import require_legacy_protocol
from ..database import database_session
from ..models import Device, DeviceBackup, User
from ..schemas import BackupCreate, validate_notebook_payload
from ..security import current_user
from ..settings import get_settings


router = APIRouter(prefix="/v1/backups", tags=["backups"])
logger = logging.getLogger(__name__)
# One worker has a 700 MiB memory limit. Validate large JSON snapshots
# serially to avoid simultaneous 64 MiB deserializations exhausting memory.
_finalize_lock = asyncio.Lock()


def backup_root() -> Path:
    root = get_settings().data_path / "backups"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


async def owned_backup(
    backup_id: uuid.UUID, owner_id: uuid.UUID, session: AsyncSession, *, lock: bool = False
) -> DeviceBackup:
    statement = select(DeviceBackup).where(
        DeviceBackup.id == backup_id,
        DeviceBackup.owner_id == owner_id,
    )
    if lock:
        statement = statement.with_for_update()
    result = await session.execute(statement)
    backup = result.scalar_one_or_none()
    if backup is None:
        raise HTTPException(status_code=404, detail="Backup not found.")
    return backup


@router.post("", status_code=201)
async def create_backup(
    body: BackupCreate,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    settings = get_settings()
    if body.sizeBytes > settings.maximum_backup_bytes:
        raise HTTPException(status_code=413, detail="Backup is too large.")
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())
    await require_legacy_protocol(session, user.id)
    device = await session.get(Device, body.deviceId)
    now = datetime.now(UTC)
    if device is None:
        device = Device(
            id=body.deviceId,
            owner_id=user.id,
            kind="backup",
            last_seen_at=now,
        )
        session.add(device)
    elif device.owner_id != user.id:
        raise HTTPException(status_code=403, detail="Device belongs to another account.")
    else:
        device.last_seen_at = now

    backup_id = uuid.uuid4()
    storage_key = f"{user.id}/{backup_id}.json"
    backup = DeviceBackup(
        id=backup_id,
        owner_id=user.id,
        device_id=device.id,
        storage_key=storage_key,
        sha256=body.sha256,
        size_bytes=body.sizeBytes,
        format_version=body.formatVersion,
        status="pending",
    )
    session.add(backup)
    await session.commit()
    return {"backupId": str(backup_id), "status": "pending"}


@router.put("/{backup_id}/content", status_code=204)
async def upload_content(
    backup_id: uuid.UUID,
    request: Request,
    content_sha256: str = Header(alias="X-Content-SHA256"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> None:
    backup = await owned_backup(backup_id, user.id, session, lock=True)
    await require_legacy_protocol(session, user.id)
    if backup.status != "pending":
        raise HTTPException(status_code=409, detail="Backup is not pending.")
    if content_sha256 != backup.sha256:
        raise HTTPException(status_code=422, detail="Checksum header mismatch.")
    root = backup_root()
    target = root / backup.storage_key
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = target.with_suffix(".upload")
    digest = hashlib.sha256()
    written = 0
    descriptor = await asyncio.to_thread(
        os.open,
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    file = os.fdopen(descriptor, "wb")
    try:
        async for chunk in request.stream():
            written += len(chunk)
            if written > backup.size_bytes or written > get_settings().maximum_backup_bytes:
                raise HTTPException(status_code=413, detail="Backup is too large.")
            digest.update(chunk)
            await asyncio.to_thread(file.write, chunk)
        await asyncio.to_thread(file.flush)
        await asyncio.to_thread(os.fsync, file.fileno())
    except Exception:
        await asyncio.to_thread(file.close)
        temporary.unlink(missing_ok=True)
        raise
    await asyncio.to_thread(file.close)
    if written != backup.size_bytes or digest.hexdigest() != backup.sha256:
        temporary.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="Backup checksum or size mismatch.")


@router.post("/{backup_id}/finalize")
async def finalize_backup(
    backup_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    async with _finalize_lock:
        await require_legacy_protocol(session, user.id)
        return await _finalize_backup(backup_id, user, session)


async def _finalize_backup(
    backup_id: uuid.UUID, user: User, session: AsyncSession,
) -> dict:
    backup = await owned_backup(backup_id, user.id, session, lock=True)
    if backup.status != "pending":
        raise HTTPException(status_code=409, detail="Backup is not pending.")
    target = backup_root() / backup.storage_key
    temporary = target.with_suffix(".upload")
    if not temporary.is_file():
        raise HTTPException(status_code=409, detail="Backup content is missing.")
    try:
        raw = await asyncio.to_thread(temporary.read_bytes)
        envelope = json.loads(raw)
        validate_envelope(envelope, backup, original_json=raw)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        logger.warning("Rejected backup %s: %s", backup.id, error)
        backup.status = "rejected"
        await session.commit()
        temporary.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="Invalid backup envelope.") from error
    await asyncio.to_thread(os.replace, temporary, target)
    backup.status = "verified"
    backup.verified_at = datetime.now(UTC)
    await session.commit()
    return {
        "backupId": str(backup.id),
        "status": "verified",
        "sha256": backup.sha256,
        "verifiedAt": backup.verified_at.isoformat(),
    }


def original_payload_json(raw: bytes) -> memoryview:
    """Read the payload's wire bytes without reparsing the full backup.

    The caller has already validated the complete JSON using json.loads.
    Dart and Python may serialize equivalent JSON numbers differently.
    """
    offset = 0
    while offset < len(raw) and raw[offset] in b" \t\r\n":
        offset += 1
    if offset >= len(raw) or raw[offset] != ord("{"):
        raise ValueError("backup envelope must be an object")
    offset += 1
    payload_range: tuple[int, int] | None = None

    while offset < len(raw):
        while offset < len(raw) and raw[offset] in b" \t\r\n":
            offset += 1
        if offset < len(raw) and raw[offset] == ord("}"):
            break
        if offset >= len(raw) or raw[offset] != ord('"'):
            raise ValueError("invalid envelope key")
        key_start = offset
        offset += 1
        while offset < len(raw):
            if raw[offset] == ord("\\"):
                offset += 2
            elif raw[offset] == ord('"'):
                offset += 1
                break
            else:
                offset += 1
        key = json.loads(raw[key_start:offset])
        while offset < len(raw) and raw[offset] in b" \t\r\n":
            offset += 1
        if offset >= len(raw) or raw[offset] != ord(":"):
            raise ValueError("invalid envelope separator")
        offset += 1
        while offset < len(raw) and raw[offset] in b" \t\r\n":
            offset += 1
        value_start = offset
        depth = 0
        quoted = False
        while offset < len(raw):
            character = raw[offset]
            if quoted:
                if character == ord("\\"):
                    offset += 2
                    continue
                if character == ord('"'):
                    quoted = False
            elif character == ord('"'):
                quoted = True
            elif character in (ord("{"), ord("[")):
                depth += 1
            elif character in (ord("}"), ord("]")):
                if depth == 0:
                    break
                depth -= 1
            elif character == ord(",") and depth == 0:
                break
            offset += 1
        if key == "payload":
            end = offset
            while end > value_start and raw[end - 1] in b" \t\r\n":
                end -= 1
            payload_range = (value_start, end)
        if offset < len(raw) and raw[offset] == ord(","):
            offset += 1
        elif offset < len(raw) and raw[offset] == ord("}"):
            break
        else:
            raise ValueError("invalid envelope delimiter")

    if payload_range is None:
        raise ValueError("missing backup payload")
    start, end = payload_range
    return memoryview(raw)[start:end]


def validate_envelope(
    envelope: object, backup: DeviceBackup, *, original_json: bytes | None = None
) -> None:
    if not isinstance(envelope, dict):
        raise ValueError("backup envelope must be an object")
    if (
        envelope.get("format") != "nanotateczki-server-backup"
        or envelope.get("version") != 2
        or backup.format_version != 2
        or envelope.get("deviceId") != str(backup.device_id)
        or envelope.get("checksumAlgorithm") != "sha256"
        or not isinstance(envelope.get("payload"), dict)
    ):
        raise ValueError("unsupported envelope")
    # Legacy v2 snapshots remain readable. New snapshots bind their
    # schema and durable operation frontier to the authenticated owner.
    metadata_keys = ("accountId", "schemaVersion", "operationCheckpoint")
    if any(key in envelope for key in metadata_keys):
        if not all(key in envelope for key in metadata_keys):
            raise ValueError("incomplete backup checkpoint metadata")
        if envelope["accountId"] != str(backup.owner_id):
            raise ValueError("backup account does not match owner")
        schema = envelope["schemaVersion"]
        if type(schema) is not int or not 1 <= schema <= 1_000_000:
            raise ValueError("invalid backup schema version")
        checkpoint = envelope["operationCheckpoint"]
        if not isinstance(checkpoint, dict) or len(checkpoint) > 256:
            raise ValueError("invalid operation checkpoint")
        for scope, sequence in checkpoint.items():
            if (
                not isinstance(scope, str)
                or not 1 <= len(scope) <= 128
                or type(sequence) is not int
                or not 0 <= sequence <= 9_007_199_254_740_991
            ):
                raise ValueError("invalid operation checkpoint entry")
    payload = envelope["payload"]
    if (
        not isinstance(payload.get("notebooks"), list)
        or not isinstance(payload.get("libraryState"), dict)
        or not isinstance(payload.get("studyPlannerState"), dict)
    ):
        raise ValueError("incomplete backup payload")
    # Optional metadata was added without changing backup envelope v2.
    # Reject malformed additions instead of marking them as verified.
    for key in ("appPreferences", "editorPreferences", "backupPreferences"):
        if key in payload and not isinstance(payload[key], dict):
            raise ValueError(f"invalid {key}")
    if "recentColors" in payload:
        recent = payload["recentColors"]
        if (not isinstance(recent, list) or len(recent) > 12
            or any(type(color) is not int or not 0 <= color <= 0xFFFFFFFF
                   for color in recent)):
            raise ValueError("invalid recentColors")
    if payload.get("backupPreferences"):
        from .account_settings import validate_settings

        validate_settings("backupPreferences", payload["backupPreferences"])
    seen_uids: set[str] = set()
    for notebook in payload["notebooks"]:
        if not isinstance(notebook, dict):
            raise ValueError("invalid notebook")
        validate_notebook_payload(notebook)
        uid = notebook["uid"]
        if uid in seen_uids:
            raise ValueError("duplicate notebook UID")
        seen_uids.add(uid)
    # Preserve the exact bytes signed by the Dart client. Python may rewrite
    # JSON numbers such as 1e-7 into 1e-07 when serializing the parsed payload.
    encoded = (
        original_payload_json(original_json)
        if original_json is not None
        else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    )
    if hashlib.sha256(encoded).hexdigest() != envelope.get("checksum"):
        raise ValueError("payload checksum mismatch")


@router.get("")
async def list_backups(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    result = await session.execute(
        select(DeviceBackup)
        .where(DeviceBackup.owner_id == user.id, DeviceBackup.status == "verified")
        .order_by(desc(DeviceBackup.verified_at), desc(DeviceBackup.created_at))
        .limit(100)
    )
    return {
        "backups": [
            {
                "backupId": str(item.id),
                "deviceId": str(item.device_id),
                "sha256": item.sha256,
                "sizeBytes": item.size_bytes,
                "createdAt": item.created_at.isoformat(),
                "verifiedAt": item.verified_at.isoformat() if item.verified_at else None,
            }
            for item in result.scalars()
        ]
    }


@router.get("/{backup_id}/download")
async def download_backup(
    backup_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> FileResponse:
    backup = await owned_backup(backup_id, user.id, session)
    if backup.status != "verified":
        raise HTTPException(status_code=404, detail="Verified backup not found.")
    path = backup_root() / backup.storage_key
    if not path.is_file():
        raise HTTPException(status_code=503, detail="Backup file is unavailable.")
    return FileResponse(
        path,
        media_type="application/json",
        filename=f"nanotateczki-{backup.id}.json",
        headers={"Digest": f"sha-256={backup.sha256}"},
    )
