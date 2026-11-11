"""WebSocket invalidation hints for v2 account and Share operation feeds."""
import asyncio
import json
import math
import uuid

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from ..database import session_factory
from ..models import StudyShare
from ..realtime_hub import hub
from ..settings import get_settings
from .operations import authorize
from .study_shares import ensure_active

router = APIRouter(tags=["operations"])


def validate_preview(data: object) -> dict | None:
    """Validate small, non-persistent drawing hints; reject all extra state."""
    if not isinstance(data, dict) or data.get("type") != "share_preview":
        return None
    for name in ("shareId", "documentUid", "pageId", "senderId"):
        value = data.get(name)
        if (
            not isinstance(value, str) or not 1 <= len(value) <= 128
            or any(ord(char) < 33 or ord(char) > 126 for char in value)
        ):
            return None
    try:
        uuid.UUID(data["shareId"])
        uuid.UUID(data["senderId"])
    except ValueError:
        return None
    result = {
        "type": "share_preview",
        "shareId": data["shareId"],
        "documentUid": data["documentUid"],
        "pageId": data["pageId"],
        "senderId": data["senderId"],
    }
    stroke = data.get("stroke")
    if stroke is None:
        result["stroke"] = None
        return result
    if not isinstance(stroke, dict):
        return None
    tool, color, width, points = (
        stroke.get("tool"), stroke.get("color"),
        stroke.get("width"), stroke.get("points"),
    )
    if (
        type(tool) is not int or not 0 <= tool <= 64
        or type(color) is not int or not 0 <= color <= 0xffffffff
        or type(width) not in (int, float)
        or not math.isfinite(width) or not 0 < width <= 100
        or not isinstance(points, list) or not 1 <= len(points) <= 96
    ):
        return None
    for point in points:
        if (
            not isinstance(point, list) or len(point) != 3
            or any(type(v) not in (int, float) or not math.isfinite(v)
                   for v in point)
            or abs(point[0]) > 1000000
            or abs(point[1]) > 1000000
            or not 0 <= point[2] <= 2
        ):
            return None
    result["stroke"] = {
        "tool": tool, "color": color, "width": width, "points": points,
    }
    return result


async def receive_share_previews(
    websocket: WebSocket, token: str, bound_share: uuid.UUID | None,
) -> None:
    """A preview never enters PostgreSQL, the reducer, or the ACK history."""
    last_sent = 0.0
    while True:
        raw = await websocket.receive_text()
        if len(raw) > 12000:
            continue
        try:
            preview = validate_preview(json.loads(raw))
        except (ValueError, TypeError):
            continue
        if preview is None:
            continue
        now = asyncio.get_running_loop().time()
        if now - last_sent < 0.06:
            continue
        share_id = uuid.UUID(preview["shareId"])
        if bound_share is not None and share_id != bound_share:
            continue
        async with session_factory() as session:
            principal = await authorize(
                session, "Bearer " + token, bound_share,
            )
            share = await session.get(StudyShare, share_id)
            if (
                share is None or share.owner_id != principal.owner_id
                or share.document_uid != preview["documentUid"]
                or (share.edit_clock or {}).get("_protocol") != "v2"
            ):
                continue
            ensure_active(share)
        last_sent = now
        # Host watches the owner feed, guests watch the share feed.
        await hub.publish(share.owner_id, preview)
        await hub.publish(share.id, preview)



@router.websocket("/v2/realtime")
async def watch_operations(websocket: WebSocket) -> None:
    origin = websocket.headers.get("origin")
    if origin is not None and origin != str(get_settings().app_origin).rstrip("/"):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    queue = None
    key = None
    try:
        message = json.loads(
            await asyncio.wait_for(websocket.receive_text(), timeout=5)
        )
        if not isinstance(message, dict) or message.get("type") != "authenticate":
            await websocket.close(code=4401)
            return
        token = message.get("token")
        share_value = message.get("shareId")
        if not isinstance(token, str):
            await websocket.close(code=4401)
            return
        try:
            share_id = uuid.UUID(share_value) if share_value is not None else None
        except (TypeError, ValueError, AttributeError):
            await websocket.close(code=4401)
            return
        async with session_factory() as session:
            principal = await authorize(session, "Bearer " + token, share_id)
        key = principal.share_id or principal.owner_id
        queue = await hub.subscribe(key)
        # Recheck after subscription: revocations must not be missed between
        # initial authentication and registration for invalidations.
        async with session_factory() as session:
            await authorize(session, "Bearer " + token, share_id)
        await websocket.send_json({"type": "authenticated"})
        reader = asyncio.create_task(
            receive_share_previews(websocket, token, share_id)
        )
        try:
            while True:
                next_event = asyncio.create_task(queue.get())
                try:
                    done, _ = await asyncio.wait(
                        {next_event, reader},
                        timeout=20,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if not next_event.done():
                        next_event.cancel()
                        await asyncio.gather(
                            next_event, return_exceptions=True,
                        )
                if reader in done:
                    await reader
                    return
                event = next_event.result() if next_event in done else {
                    "type": "ping",
                }
                if event.get("type") == "ended":
                    await websocket.send_json(event)
                    return
                async with session_factory() as session:
                    await authorize(session, "Bearer " + token, share_id)
                await websocket.send_json(event)
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
    except (
        asyncio.TimeoutError, WebSocketDisconnect, json.JSONDecodeError,
        HTTPException, RuntimeError,
    ):
        try:
            await websocket.close(code=4401)
        except RuntimeError:
            pass
    finally:
        if queue is not None and key is not None:
            await hub.unsubscribe(key, queue)
