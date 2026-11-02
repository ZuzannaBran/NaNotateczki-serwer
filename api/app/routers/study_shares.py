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
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import database_session, session_factory
from ..models import ChangeEvent, StudyShare, StudyShareGrant, SyncedNotebook, User
from ..rate_limit import InMemoryRateLimiter
from ..realtime_hub import hub
from ..schemas import validate_notebook_payload
from ..security import current_user, hash_password, token_digest, verify_password
from .sync import lock_owner_events
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


class ShareDocumentWrite(BaseModel):
    baseRevision: int = Field(ge=1)
    baseNotebook: dict
    notebook: dict

    @field_validator("baseNotebook", "notebook")
    @classmethod
    def validate_document(cls, value: dict) -> dict:
        validate_notebook_payload(value)
        return value


def _items_by_id(items: list[dict]) -> dict[str, dict]:
    result = {}
    for item in items:
        uid = item.get("id")
        if not isinstance(uid, str) or not uid or uid in result:
            raise HTTPException(status_code=422, detail="Duplicate or invalid object ID.")
        result[uid] = item
    return result



def _size_of_change(before: object, after: object) -> int:
    original = json.dumps(before, sort_keys=True, separators=(",", ":")).encode()
    changed = json.dumps(after, sort_keys=True, separators=(",", ":")).encode()
    return abs(len(original) - len(changed)) + sum(
        left != right for left, right in zip(original, changed)
    )


def _first_change_wins(
    key: str, before: object, after: object,
    actual: object, clock: dict, received_at: datetime,
) -> bool:
    """Order by server receipt time, not client clocks.

    If both writes arrived in the same microsecond, the larger edit wins.
    A committed owner change without a share clock always takes precedence
    over a stale guest edit.
    """
    if actual == after:
        return False
    score = _size_of_change(before, after)
    previous = clock.get(key)
    if actual != before and actual != after:
        if not isinstance(previous, dict):
            return False
        earlier = datetime.fromisoformat(previous["at"])
        if earlier < received_at:
            return False
        if earlier == received_at and int(previous["size"]) >= score:
            return False
    clock[key] = {"at": received_at.isoformat(), "size": score}
    return True


def _merge_list(
    base: list[dict], changed: list[dict], latest: list[dict],
    clock: dict, prefix: str, received_at: datetime,
) -> list[dict]:
    old = _items_by_id(base)
    new = _items_by_id(changed)
    current = _items_by_id(latest)
    merged = dict(current)
    for uid in sorted(old.keys() | new.keys()):
        before = old.get(uid)
        after = new.get(uid)
        if before == after:
            continue
        actual = current.get(uid)
        if _first_change_wins(
            f"{prefix}/{uid}", before, after, actual, clock, received_at
        ):
            if after is None:
                merged.pop(uid, None)
            else:
                merged[uid] = after
    return list(merged.values())


def _merge_page(
    base: dict, changed: dict, latest: dict,
    clock: dict, received_at: datetime,
) -> dict:
    result = dict(latest)
    prefix = f"page/{base['id']}"
    for key in sorted(base.keys() | changed.keys()):
        if key == "id":
            continue
        if key in ("inkStrokes", "textBlocks", "imageBlocks"):
            result[key] = _merge_list(
                base.get(key, []), changed.get(key, []), latest.get(key, []),
                clock, f"{prefix}/{key}", received_at,
            )
        elif base.get(key) != changed.get(key):
            if _first_change_wins(
                f"{prefix}/{key}", base.get(key), changed.get(key),
                latest.get(key), clock, received_at,
            ):
                result[key] = changed.get(key)
    return result



async def require_verified_base(
    session: AsyncSession,
    record: SyncedNotebook,
    revision: int,
    submitted: dict,
) -> None:
    """Never trust a client's claimed common ancestor in a shared edit."""
    if revision > record.revision:
        raise HTTPException(status_code=409, detail="Unknown base revision.")
    if revision == record.revision:
        authoritative = record.content
    else:
        authoritative = await session.scalar(
            select(ChangeEvent.content)
            .where(
                ChangeEvent.owner_id == record.owner_id,
                ChangeEvent.entity_uid == record.uid,
                ChangeEvent.revision == revision,
                ChangeEvent.deleted.is_(False),
            )
            .order_by(ChangeEvent.id.desc())
            .limit(1)
        )
    if authoritative is None or authoritative != submitted:
        raise HTTPException(
            status_code=409, detail="Unverified document base revision."
        )


def merge_share_document(
    base: dict, changed: dict, latest: dict,
    clock: dict | None = None, received_at: datetime | None = None,
) -> dict:
    # A guest may edit the content, never relocate, rename, or re-own it.
    immutable = ("uid", "kind", "folder", "title", "createdAt")
    if any(changed.get(key) != base.get(key) for key in immutable):
        raise HTTPException(status_code=409, detail="Document metadata changed.")
    received_at = received_at or datetime.now(UTC)
    clock = clock if clock is not None else {}
    original = _items_by_id(base["pages"])
    submitted = _items_by_id(changed["pages"])
    current = _items_by_id(latest["pages"])
    combined = dict(current)
    for uid in sorted(original.keys() | submitted.keys()):
        before = original.get(uid)
        after = submitted.get(uid)
        actual = current.get(uid)
        if before == after:
            continue
        if before is None or after is None:
            if _first_change_wins(
                f"page/{uid}/structure", before, after,
                actual, clock, received_at,
            ):
                if after is None:
                    combined.pop(uid, None)
                else:
                    combined[uid] = after
        elif actual is not None:
            combined[uid] = _merge_page(
                before, after, actual, clock, received_at,
            )
        # A concurrent deletion of this page cannot be undone by a stale
        # update. The guest can create a new page with a distinct id.
    merged = dict(latest)
    merged["pages"] = list(combined.values())
    try:
        last_update = datetime.fromisoformat(
            latest["updatedAt"].replace("Z", "+00:00")
        )
        if last_update.tzinfo is None:
            last_update = last_update.replace(tzinfo=UTC)
    except (ValueError, TypeError, KeyError) as error:
        raise HTTPException(status_code=422, detail="Invalid timestamp.") from error
    # Avoid touching updatedAt for rejected or duplicate changes.
    if merged["pages"] != latest["pages"]:
        merged["updatedAt"] = max(
            datetime.now(UTC), last_update + timedelta(microseconds=1)
        ).isoformat()
    validate_notebook_payload(merged)
    return merged


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
    # Prevent races with owner deletion and concurrent share creation.
    await lock_owner_events(session, user.id)
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
    # Only one conflict clock may govern an active document.
    existing = await session.scalar(
        select(StudyShare)
        .where(
            StudyShare.owner_id == user.id,
            StudyShare.document_uid == body.documentUid,
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
        title=str(notebook.content.get("title") or body.title)[:200],
        password_hash=hash_password(body.password),
        revision=0,
        expires_at=datetime.now(UTC) + timedelta(hours=4),
    )
    session.add(share)
    await session.commit()
    return {"id": str(share.id), "expiresAt": share.expires_at.isoformat()}



@router.get("/v1/study-shares/active")
async def active_shares(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> dict:
    rows = await session.scalars(
        select(StudyShare)
        .where(StudyShare.owner_id == user.id,
               StudyShare.revoked_at.is_(None),
               StudyShare.expires_at > datetime.now(UTC))
        .order_by(StudyShare.created_at.desc())
    )
    return {"sessions": [{
        "id": str(share.id), "documentUid": share.document_uid,
        "title": share.title, "expiresAt": share.expires_at.isoformat(),
    } for share in rows]}


@router.get("/v1/study-shares/{share_id}/document")
async def read_shared_document(
    share_id: uuid.UUID,
    ticket: str = Depends(guest_ticket),
    session: AsyncSession = Depends(database_session),
) -> dict:
    share = await guest_share(share_id, ticket, session)
    notebook = await session.scalar(
        select(SyncedNotebook).where(
            SyncedNotebook.owner_id == share.owner_id,
            SyncedNotebook.uid == share.document_uid,
            SyncedNotebook.deleted_at.is_(None),
        )
    )
    if notebook is None or not isinstance(notebook.content, dict):
        raise HTTPException(status_code=410, detail="The shared document is unavailable.")
    return {"notebook": notebook.content, "revision": notebook.revision,
            "expiresAt": share.expires_at.isoformat()}


@router.put("/v1/study-shares/{share_id}/document")
async def edit_shared_document(
    share_id: uuid.UUID,
    body: ShareDocumentWrite,
    request: Request,
    ticket: str = Depends(guest_ticket),
    session: AsyncSession = Depends(database_session),
) -> dict:
    length = request.headers.get("content-length", "")
    if not length.isdigit() or int(length) > get_settings().maximum_sync_bytes:
        raise HTTPException(status_code=413, detail="Document is too large.")
    received_at = datetime.now(UTC)
    share = await guest_share(share_id, ticket, session)
    await lock_owner_events(session, share.owner_id)
    # Lock the share itself as well, serializing revocation and guest writes.
    share = await session.scalar(select(StudyShare)
        .where(StudyShare.id == share_id).with_for_update())
    ensure_active(share)
    notebook = await session.scalar(
        select(SyncedNotebook).where(
            SyncedNotebook.owner_id == share.owner_id,
            SyncedNotebook.uid == share.document_uid,
            SyncedNotebook.deleted_at.is_(None),
        ).with_for_update()
    )
    if notebook is None or not isinstance(notebook.content, dict):
        raise HTTPException(status_code=410, detail="Document is unavailable.")
    if (body.baseNotebook.get("uid") != share.document_uid or
            body.notebook.get("uid") != share.document_uid or
            body.baseRevision > notebook.revision):
        raise HTTPException(status_code=409, detail="Document revision mismatch.")
    await require_verified_base(
        session, notebook, body.baseRevision, body.baseNotebook
    )
    clock = dict(share.edit_clock or {})
    changed = merge_share_document(
        body.baseNotebook, body.notebook, notebook.content,
        clock, received_at,
    )
    if changed == notebook.content:
        return {"notebook": notebook.content, "revision": notebook.revision}
    share.edit_clock = clock
    notebook.revision += 1
    notebook.content = changed
    notebook.updated_at = datetime.now(UTC)
    event = ChangeEvent(
        owner_id=share.owner_id, entity_uid=share.document_uid,
        revision=notebook.revision, deleted=False, content=changed,
    )
    session.add(event)
    await session.flush()
    await session.commit()
    await hub.publish(share.owner_id,
                      {"type": "notebook_changed", "cursor": event.id})
    await hub.publish(share_id,
                      {"type": "document_changed", "revision": notebook.revision})
    return {"notebook": changed, "revision": notebook.revision}


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
    origin = websocket.headers.get("origin")
    # Browsers send Origin; native clients may omit it but still need a ticket.
    if origin is not None and origin != str(get_settings().app_origin).rstrip("/"):
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
