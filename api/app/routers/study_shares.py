import asyncio
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import (
    APIRouter, Depends, Header, HTTPException, Request, Response,
    WebSocket, WebSocketDisconnect,
)
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..account_protocol import require_v2_protocol
from ..database import database_session, session_factory
from ..models import (
    DocumentState, StudyShare, StudyShareGrant, User,
)
from ..operation_rollout import require_operations_v2_write
from ..rate_limit import InMemoryRateLimiter
from ..realtime_hub import hub
from ..security import current_user, hash_password, token_digest, verify_password
from ..owner_lock import lock_owner_events
from ..settings import get_settings
from ..browser_origins import browser_origin_allowed


router = APIRouter(tags=["study-shares"])
join_limiter = InMemoryRateLimiter(attempts=8, window_seconds=300)


class ShareCreate(BaseModel):
    documentUid: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=128)


class ShareJoin(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class ShareAppearanceWrite(BaseModel):
    darkMode: bool
    backgroundStyle: int = Field(ge=0, le=2)
    backgroundSpacing: float = Field(ge=16, le=64)


def share_appearance(share: StudyShare) -> dict | None:
    clock = share.edit_clock or {}
    value = clock.get("_appearance")
    return value if isinstance(value, dict) else None


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


def ensure_operational(share: StudyShare) -> None:
    if (share.edit_clock or {}).get("_protocol") != "v2":
        raise HTTPException(status_code=410, detail="Legacy sharing session retired.")


def ensure_active(share: StudyShare) -> None:
    ensure_operational(share)
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


@router.post("/v2/study-shares", status_code=201)
async def create_v2_share(
    body: ShareCreate,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    """Share only a committed operational document on a fenced v2 account."""
    require_operations_v2_write(user.id)
    await lock_owner_events(session, user.id)
    await require_v2_protocol(session, user.id)
    document = await session.scalar(
        select(DocumentState).where(
            DocumentState.owner_id == user.id,
            DocumentState.document_uid == body.documentUid,
            DocumentState.deleted_at.is_(None),
        )
    )
    if document is None or not isinstance(document.content, dict):
        raise HTTPException(
            status_code=409,
            detail="Commit the v2 document before sharing.",
        )
    properties = document.content.get("properties")
    if not isinstance(properties, dict):
        raise HTTPException(status_code=409, detail="Corrupt v2 document.")
    kind = properties.get("kindIndex")
    if type(kind) is not int or kind not in (0, 1):
        raise HTTPException(
            status_code=422, detail="Only notebooks and boards can be shared."
        )
    existing = await session.scalar(
        select(StudyShare)
        .where(
            StudyShare.owner_id == user.id,
            StudyShare.document_uid == body.documentUid,
            StudyShare.edit_clock["_protocol"].as_string() == "v2",
            StudyShare.revoked_at.is_(None),
            StudyShare.expires_at > datetime.now(UTC),
        )
        .limit(1)
        .with_for_update()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="This document already has an active sharing session.",
        )
    share = StudyShare(
        id=uuid.uuid4(),
        owner_id=user.id,
        document_uid=body.documentUid,
        title=str(properties.get("title") or body.title)[:200],
        password_hash=hash_password(body.password),
        revision=0,
        edit_clock={"_protocol": "v2"},
        expires_at=datetime.now(UTC) + timedelta(hours=4),
    )
    session.add(share)
    await session.commit()
    return {"id": str(share.id), "expiresAt": share.expires_at.isoformat()}


@router.get("/v2/study-shares/active")
async def active_shares(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    rows = await session.scalars(
        select(StudyShare)
        .where(StudyShare.owner_id == user.id,
               StudyShare.edit_clock["_protocol"].as_string() == "v2",
               StudyShare.revoked_at.is_(None),
               StudyShare.expires_at > datetime.now(UTC))
        .order_by(StudyShare.created_at.desc())
    )
    return {"sessions": [{
        "id": str(share.id), "documentUid": share.document_uid,
        "title": share.title, "expiresAt": share.expires_at.isoformat(),
        "protocol": (share.edit_clock or {}).get("_protocol", "v1"),
    } for share in rows]}


@router.put("/v2/study-shares/{share_id}/appearance")
async def update_share_appearance(
    share_id: uuid.UUID,
    body: ShareAppearanceWrite,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    require_operations_v2_write(user.id)
    # Serialized with guest edits by the same row lock; never changes the
    # document revision or its conflict clock.
    share = await owner_share(share_id, user, session, lock=True)
    ensure_active(share)
    appearance = body.model_dump()
    if share_appearance(share) == appearance:
        return {"appearance": appearance}
    share.edit_clock = {**(share.edit_clock or {}), "_appearance": appearance}
    await session.commit()
    await hub.publish(share_id, {"type": "appearance_changed"})
    return {"appearance": appearance}


@router.get("/v2/study-shares/{share_id}/appearance")
async def read_share_appearance(
    share_id: uuid.UUID,
    ticket: str = Depends(guest_ticket),
    session: AsyncSession = Depends(database_session),
) -> dict:
    share = await guest_share(share_id, ticket, session)
    return {"appearance": share_appearance(share)}


@router.post("/v2/study-shares/{share_id}/join")
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
    require_operations_v2_write(share.owner_id)
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
        "documentUid": share.document_uid,
        "protocol": (share.edit_clock or {}).get("_protocol", "v1"),
    }


@router.post("/v2/study-shares/{share_id}/extend")
async def extend_share(
    share_id: uuid.UUID,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    require_operations_v2_write(user.id)
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

@router.delete("/v2/study-shares/{share_id}", status_code=204)
async def end_share(
    share_id: uuid.UUID, user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> Response:
    share = await owner_share(share_id, user, session, lock=True)
    ensure_operational(share)
    share.revoked_at = datetime.now(UTC)
    await session.commit()
    await hub.publish(share_id, {"type": "ended"})
    return Response(status_code=204)


@router.websocket("/v2/study-shares/{share_id}/events")
async def watch_share(websocket: WebSocket, share_id: uuid.UUID) -> None:
    origin = websocket.headers.get("origin")
    # Browsers send Origin; native clients may omit it but still need a ticket.
    if origin is not None and not browser_origin_allowed(origin, get_settings()):
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
        # Subscribe before authorizing. A revocation between authorization
        # and subscription would otherwise lose the "ended" event, leaving
        # the guest connected until the next periodic permission check.
        queue = await hub.subscribe(share_id)
        async with session_factory() as session:
            share = await guest_share(share_id, ticket, session)
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
