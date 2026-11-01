import asyncio
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import database_session
from ..models import Device, DeviceBackup, User
from ..schemas import BackupCreate, validate_notebook_payload
from ..security import current_user
from ..settings import get_settings


router = APIRouter(prefix="/v1/backups", tags=["backups"])


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
        validate_envelope(envelope, backup)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
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


def validate_envelope(envelope: object, backup: DeviceBackup) -> None:
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
    payload = envelope["payload"]
    if (
        not isinstance(payload.get("notebooks"), list)
        or not isinstance(payload.get("libraryState"), dict)
        or not isinstance(payload.get("studyPlannerState"), dict)
    ):
        raise ValueError("incomplete backup payload")
    seen_uids: set[str] = set()
    for notebook in payload["notebooks"]:
        if not isinstance(notebook, dict):
            raise ValueError("invalid notebook")
        validate_notebook_payload(notebook)
        uid = notebook["uid"]
        if uid in seen_uids:
            raise ValueError("duplicate notebook UID")
        seen_uids.add(uid)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
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
        .order_by(desc(DeviceBackup.created_at))
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
