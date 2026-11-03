"""WebSocket invalidation hints for v2 account and Share operation feeds."""
import asyncio
import json
import uuid

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from ..database import session_factory
from ..realtime_hub import hub
from ..settings import get_settings
from .operations import authorize

router = APIRouter(tags=["operations"])


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
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
            except TimeoutError:
                event = {"type": "ping"}
            if event.get("type") == "ended":
                await websocket.send_json(event)
                return
            async with session_factory() as session:
                await authorize(session, "Bearer " + token, share_id)
            await websocket.send_json(event)
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
