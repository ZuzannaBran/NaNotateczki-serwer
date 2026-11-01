import asyncio
import json
import os
import secrets
import struct
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import (
    APIRouter, Depends, Header, HTTPException, Request, Response,
    WebSocket, WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import database_session, session_factory
from ..models import StudyShare, StudyShareGrant, SyncedNotebook, User
from ..rate_limit import InMemoryRateLimiter
from ..realtime_hub import hub
from ..security import current_user, hash_password, token_digest, verify_password
from ..settings import get_settings


router = APIRouter(tags=["study-shares"])
join_limiter = InMemoryRateLimiter(attempts=8, window_seconds=300)
publish_limiter = InMemoryRateLimiter(attempts=45, window_seconds=60)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_FRAME_BYTES = 2 * 1024 * 1024


class ShareCreate(BaseModel):
    documentUid: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=128)


class ShareJoin(BaseModel):
    password: str = Field(min_length=1, max_length=128)


def validate_png_frame(data: bytes) -> None:
    if len(data) < 33 or len(data) > MAX_FRAME_BYTES:
        raise HTTPException(status_code=413, detail="Invalid frame size.")
    if data[:8] != PNG_MAGIC or data[12:16] != b"IHDR":
        raise HTTPException(status_code=422, detail="Invalid PNG frame.")
    width, height = struct.unpack(">II", data[16:24])
    if not (1 <= width <= 4096 and 1 <= height <= 4096):
        raise HTTPException(status_code=422, detail="Invalid PNG dimensions.")


def frame_path(share_id: uuid.UUID) -> Path:
    return get_settings().data_path / "study-shares" / f"{share_id}.png"


async def owner_share(
    share_id: uuid.UUID, user: User, session: AsyncSession, *, lock: bool = False
) -> StudyShare:
    query = select(StudyShare).where(
        StudyShare.id == share_id, StudyShare.owner_id == user.id
    )
    if lock:
        query = query.with_for_update()
    share = await session.scalar(query)
    if share is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return share


def ensure_active(share: StudyShare) -> None:
    if share.revoked_at is not None or share.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=410, detail="Session has ended.")


async def guest_share(
    share_id: uuid.UUID, ticket: str, session: AsyncSession
) -> StudyShare:
    if len(ticket) < 40 or len(ticket) > 256:
        raise HTTPException(status_code=401, detail="Invalid session ticket.")
    grant = await session.scalar(
        select(StudyShareGrant).where(
            StudyShareGrant.share_id == share_id,
            StudyShareGrant.token_hash == token_digest(ticket),
            StudyShareGrant.expires_at > datetime.now(UTC),
        )
    )
    if grant is None:
        raise HTTPException(status_code=401, detail="Session ticket expired.")
    share = await session.get(StudyShare, share_id)
    if share is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    ensure_active(share)
    return share


def guest_ticket(authorization: str | None = Header(default=None)) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Session ticket required.")
    return authorization[7:]


@router.post("/v1/study-shares", status_code=201)
async def create_share(
    body: ShareCreate,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    # A session is an overlay on an existing, owner-owned cloud document.
    # It must never become the only persisted copy of a user's work.
    notebook = await session.scalar(
        select(SyncedNotebook).where(
            SyncedNotebook.owner_id == user.id,
            SyncedNotebook.uid == body.documentUid,
            SyncedNotebook.deleted_at.is_(None),
        )
    )
    if notebook is None:
        raise HTTPException(
            status_code=409,
            detail="Save this document to your account before sharing.",
        )
    if not isinstance(notebook.content, dict) or notebook.content.get("kind") not in (0, 1):
        raise HTTPException(
            status_code=422, detail="Only notebooks and boards can be shared."
        )
    share = StudyShare(
        id=uuid.uuid4(),
        owner_id=user.id,
        document_uid=body.documentUid,
        title=str(notebook.content.get("title") or body.title)[:200],
        password_hash=hash_password(body.password),
        revision=0,
        expires_at=datetime.now(UTC) + timedelta(hours=4),
    )
    session.add(share)
    await session.commit()
    return {"id": str(share.id), "expiresAt": share.expires_at.isoformat()}


@router.post("/v1/study-shares/{share_id}/join")
async def join_share(
    share_id: uuid.UUID, body: ShareJoin, request: Request,
    session: AsyncSession = Depends(database_session),
) -> dict:
    client = request.client.host if request.client else "unknown"
    await join_limiter.check(f"{client}:{share_id}")
    share = await session.get(StudyShare, share_id)
    if share is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    ensure_active(share)
    if not verify_password(body.password, share.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect session password.")
    ticket = secrets.token_urlsafe(48)
    session.add(StudyShareGrant(
        id=uuid.uuid4(),
        share_id=share_id,
        token_hash=token_digest(ticket),
        expires_at=min(share.expires_at, datetime.now(UTC) + timedelta(hours=4)),
    ))
    await session.commit()
    return {
        "ticket": ticket,
        "title": share.title,
        "revision": share.revision,
        "expiresAt": share.expires_at.isoformat(),
    }


@router.put("/v1/study-shares/{share_id}/frame", status_code=204)
async def publish_frame(
    share_id: uuid.UUID, request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> Response:
    await publish_limiter.check(f"owner:{user.id}")
    if request.headers.get("content-type", "").split(";")[0] != "image/png":
        raise HTTPException(status_code=415, detail="Only PNG is supported.")
    share = await owner_share(share_id, user, session, lock=True)
    ensure_active(share)
    frame = await request.body()
    validate_png_frame(frame)
    destination = frame_path(share_id)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_name(f".{share_id}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as output:
            output.write(frame)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    share.revision += 1
    await session.commit()
    await hub.publish(share_id, {"type": "frame_changed", "revision": share.revision})
    return Response(status_code=204)


@router.get("/v1/study-shares/{share_id}/frame")
async def read_frame(
    share_id: uuid.UUID,
    ticket: str = Depends(guest_ticket),
    session: AsyncSession = Depends(database_session),
) -> FileResponse:
    share = await guest_share(share_id, ticket, session)
    destination = frame_path(share.id)
    if not destination.is_file():
        raise HTTPException(status_code=404, detail="Waiting for the host.")
    return FileResponse(
        destination, media_type="image/png", headers={"Cache-Control": "no-store"}
    )




@router.post("/v1/study-shares/{share_id}/extend")
async def extend_share(
    share_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    share = await owner_share(share_id, user, session, lock=True)
    ensure_active(share)
    now = datetime.now(UTC)
    if share.expires_at - now > timedelta(minutes=15):
        raise HTTPException(
            status_code=409, detail="The session is not close to expiring."
        )
    share.expires_at += timedelta(hours=4)
    await session.execute(
        update(StudyShareGrant)
        .where(StudyShareGrant.share_id == share_id)
        .values(expires_at=share.expires_at)
    )
    await session.commit()
    expires_at = share.expires_at.isoformat()
    await hub.publish(
        share_id, {"type": "extended", "expiresAt": expires_at}
    )
    return {"expiresAt": expires_at}

@router.delete("/v1/study-shares/{share_id}", status_code=204)
async def end_share(
    share_id: uuid.UUID, user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> Response:
    share = await owner_share(share_id, user, session, lock=True)
    share.revoked_at = datetime.now(UTC)
    await session.commit()
    frame_path(share_id).unlink(missing_ok=True)
    await hub.publish(share_id, {"type": "ended"})
    return Response(status_code=204)


@router.websocket("/v1/study-shares/{share_id}/events")
async def watch_share(websocket: WebSocket, share_id: uuid.UUID) -> None:
    if websocket.headers.get("origin") != str(get_settings().app_origin).rstrip("/"):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    queue = None
    try:
        message = json.loads(await asyncio.wait_for(websocket.receive_text(), 5))
        if not isinstance(message, dict) or message.get("type") != "authenticate":
            await websocket.close(code=4401)
            return
        ticket = message.get("ticket")
        if not isinstance(ticket, str):
            await websocket.close(code=4401)
            return
        async with session_factory() as session:
            share = await guest_share(share_id, ticket, session)
        queue = await hub.subscribe(share_id)
        await websocket.send_json({"type": "authenticated", "revision": share.revision})
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
            except TimeoutError:
                event = {"type": "ping"}
            # A revocation must reach already authenticated guests before
            # their tickets stop passing authorization checks.
            if event["type"] == "ended":
                await websocket.send_json(event)
                return
            async with session_factory() as session:
                await guest_share(share_id, ticket, session)
            await websocket.send_json(event)
    except (WebSocketDisconnect, TimeoutError, json.JSONDecodeError,
            HTTPException, ValueError):
        try:
            await websocket.close(code=4401)
        except RuntimeError:
            pass
    finally:
        if queue is not None:
            await hub.unsubscribe(share_id, queue)
